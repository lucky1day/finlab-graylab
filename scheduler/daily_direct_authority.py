"""日频单机 MVP 的直接静态运行 authority。"""

from __future__ import annotations

import os
import stat
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from migrations.runner import (
    MigrationPreflightError,
    preflight_schedule_run_started_at_nullable,
)
from scheduler.capacity_candidate_runtime import (
    CapacityCandidateRuntimeError,
    NATIVE_ENV_NAME,
    RUNTIME_PROFILE_PATH,
    _build_target_manifest,
    _load_and_validate_runtime_profile,
    _read_database_snapshot,
    _read_source_file,
    _require_same_discovery,
    _validate_profile_bindings,
    _validated_project_root,
)
from scheduler.daily_policy import (
    POLICY_V2_PATH,
    DailySchedulerPolicy,
    load_daily_policy,
)
from scheduler.discovery import SchemeConfig, discover_schemes
from shared.artifact_paths import safe_path_part
from shared.liwei_0616_cache_contract import (
    APPROVED_PHASE_A_CACHE_PUBLISHERS,
    PHASE_A_CACHE_ABI_VERSION,
    validate_direct_cache_runtime_context,
)
from shared.liwei_0616_cache_projection import PROJECTION_SCHEMA_VERSION
from shared.liwei_0616_phase_a_cache import (
    DEFAULT_CACHE_ROOT,
    GENERATION_MANIFEST_SCHEMA_VERSION,
    INPUT_GENERATION_STATE_SCHEMA_VERSION,
    _load_generation_directory,
    _load_current_generation,
    _validate_generation_acceptance_for_use,
    _verify_generation_acceptance_lineage,
)
from shared.native_input_generation import (
    NATIVE_GENERATION_EXPORTER_VERSION,
    NATIVE_GENERATION_SCHEMA_VERSION,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIRECT_AUTHORITY_SCHEMA_VERSION = "daily-direct-cache-authorities-v1"
EXPECTED_NATIVE_COUNT = 17
EXPECTED_BLACKBOX_COUNT = 8
EXPECTED_ITEM_COUNT = 25
EXPECTED_TARGET_COUNT = 29


class DailyDirectAuthorityError(RuntimeError):
    """当前 policy/Registry/cache 无法形成 exact direct authority。"""


def build_daily_direct_cache_authorities(
    engine: object,
    *,
    project_root: str | Path = PROJECT_ROOT,
    policy_path: str | Path = POLICY_V2_PATH,
    discovered: Iterable[SchemeConfig] | None = None,
    algo_env: str = NATIVE_ENV_NAME,
    cache_root: str | Path | None = None,
) -> dict[str, object]:
    """只读重算当前日频静态身份并冻结 direct cache authority。"""
    if algo_env != NATIVE_ENV_NAME:
        raise DailyDirectAuthorityError(
            "daily Native algorithm environment must remain "
            f"{NATIVE_ENV_NAME!r}"
        )
    root = _validated_project_root(Path(project_root))
    policy_file = (
        Path(policy_path)
        if Path(policy_path).is_absolute()
        else root / Path(policy_path)
    )
    current_active = tuple(
        config
        for config in discover_schemes(
            root / "schemes",
            strict=True,
        )
        if config.status == "active" and config.frequency == "daily"
    )
    policy = load_daily_policy(
        policy_file,
        discovered=current_active,
    )
    current = tuple(
        config
        for config in current_active
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
        _require_same_discovery(supplied, current)

    try:
        preflight_schedule_run_started_at_nullable(engine)
        profile_path = root / RUNTIME_PROFILE_PATH
        profile_raw = _read_source_file(
            profile_path,
            "Blackbox runtime profile",
        )
        profile = _load_and_validate_runtime_profile(
            profile_path,
            profile_raw,
        )
        _validate_profile_bindings(
            current,
            profile_name=profile.name,
            conda_env=profile.conda_env,
            data_schema_version=profile.data_schema_version,
            contract_version=profile.contract_version,
        )
        database = _read_database_snapshot(engine)
        current_registry_versions = (
            _select_direct_current_registry_versions(
                current,
                database["registry_versions"],
            )
        )
        target_manifest, _bundle = _build_target_manifest(
            root,
            current,
            policy,
            current_registry_versions,
        )
    except (
        CapacityCandidateRuntimeError,
        MigrationPreflightError,
    ) as exc:
        raise DailyDirectAuthorityError(
            f"direct daily authority preflight failed: {exc}"
        ) from exc
    _validate_direct_daily_shape(
        policy=policy,
        configs=current,
        target_manifest=target_manifest,
    )
    storage_root = _direct_storage_root(cache_root)
    return _build_direct_cache_authorities(
        policy=policy,
        configs={config.scheme_id: config for config in current},
        target_manifest=target_manifest,
        storage_root=storage_root,
    )


def _select_direct_current_registry_versions(
    configs: Sequence[SchemeConfig],
    raw_rows: object,
) -> list[Mapping[str, object]]:
    """按 direct runtime 语义选择每个 Registry target 的当前版本。"""
    if not isinstance(raw_rows, list):
        raise DailyDirectAuthorityError(
            "active Registry/version query did not return rows"
        )
    config_by_id = {config.scheme_id: config for config in configs}
    if len(config_by_id) != len(configs):
        raise DailyDirectAuthorityError(
            "strict discovery contains duplicate daily schemes"
        )

    rows_by_registry: dict[str, list[Mapping[str, object]]] = {}
    for index, row in enumerate(raw_rows):
        if not isinstance(row, Mapping):
            raise DailyDirectAuthorityError(
                f"active Registry/version row {index} is not an object"
            )
        registry_id = row.get("registry_scheme_id")
        base_id = row.get("base_scheme_id")
        if not isinstance(registry_id, str) or not registry_id:
            raise DailyDirectAuthorityError(
                "active Registry/version row has invalid registry_scheme_id"
            )
        if not isinstance(base_id, str) or base_id not in config_by_id:
            raise DailyDirectAuthorityError(
                "Registry contains unknown active daily scheme: "
                f"{base_id!r}"
            )
        rows_by_registry.setdefault(registry_id, []).append(row)

    selected: list[Mapping[str, object]] = []
    for registry_id in sorted(rows_by_registry):
        rows = rows_by_registry[registry_id]
        base_ids = {row.get("base_scheme_id") for row in rows}
        if len(base_ids) != 1:
            raise DailyDirectAuthorityError(
                f"Registry target {registry_id} maps to multiple base schemes"
            )
        config = config_by_id[str(next(iter(base_ids)))]
        if config.runtime_type == "blackbox_v2":
            if len(rows) != 1:
                raise DailyDirectAuthorityError(
                    "Blackbox Registry target "
                    f"{registry_id} requires exactly one active version; "
                    f"found {len(rows)}"
                )
            selected.append(rows[0])
            continue
        if config.runtime_type != "native_adapter":
            raise DailyDirectAuthorityError(
                f"unsupported daily runtime type: {config.runtime_type}"
            )

        current_rows = [
            row
            for row in rows
            if _matches_direct_current_version(row, config)
        ]
        if len(current_rows) != 1:
            raise DailyDirectAuthorityError(
                "Native Registry target "
                f"{registry_id} requires exactly one exact current active "
                f"version; found {len(current_rows)}"
            )
        selected.append(current_rows[0])
    return selected


def _matches_direct_current_version(
    row: Mapping[str, object],
    config: SchemeConfig,
) -> bool:
    expected = {
        "scheme_version": config.scheme_version,
        "version_runtime_type": config.runtime_type,
        "code_hash": config.code_hash,
        "config_hash": config.config_hash,
        "manifest_hash": config.manifest_hash,
    }
    return all(row.get(field) == value for field, value in expected.items())


def _validate_direct_daily_shape(
    *,
    policy: object,
    configs: Sequence[object],
    target_manifest: Sequence[object],
) -> None:
    counts = Counter(
        str(getattr(config, "runtime_type", ""))
        for config in configs
    )
    if (
        getattr(policy, "expected_item_count", None)
        != EXPECTED_ITEM_COUNT
        or len(configs) != EXPECTED_ITEM_COUNT
    ):
        raise DailyDirectAuthorityError(
            "direct daily authority requires exactly 25 items"
        )
    if (
        getattr(policy, "expected_target_count", None)
        != EXPECTED_TARGET_COUNT
        or len(target_manifest) != EXPECTED_TARGET_COUNT
    ):
        raise DailyDirectAuthorityError(
            "direct daily authority requires exactly 29 targets"
        )
    if counts != {
        "native_adapter": EXPECTED_NATIVE_COUNT,
        "blackbox_v2": EXPECTED_BLACKBOX_COUNT,
    }:
        raise DailyDirectAuthorityError(
            "direct daily authority requires exactly 17 Native and "
            "8 Blackbox items"
        )
    if (
        getattr(policy, "native_max_concurrency", None) != 2
        or getattr(policy, "v2_max_concurrency", None) != 2
    ):
        raise DailyDirectAuthorityError(
            "direct daily authority requires fixed 2+2 concurrency"
        )


def _direct_storage_root(value: str | Path | None) -> Path:
    configured = (
        value
        if value is not None
        else os.getenv("LIWEI_0616_PHASE_A_CACHE_ROOT")
        or DEFAULT_CACHE_ROOT
    )
    root = Path(configured)
    if (
        not root.is_absolute()
        or os.path.normpath(os.fspath(root)) != os.fspath(root)
        or ".." in root.parts
    ):
        raise DailyDirectAuthorityError(
            "direct cache storage root must be absolute and normalized"
        )
    return root


def _require_direct_cache_ancestry(
    storage_root: Path,
    family_root: Path,
) -> None:
    """精确校验 direct cache root、namespace 与 family 权限。"""
    if family_root.parent.parent != storage_root:
        raise DailyDirectAuthorityError(
            "direct cache family escapes storage root"
        )
    _require_direct_cache_directory(
        storage_root,
        "direct cache storage root",
        exact_mode_0700=True,
    )
    _require_direct_cache_directory(
        family_root.parent,
        "direct cache family namespace",
        exact_mode_0700=False,
    )
    _require_direct_cache_directory(
        family_root,
        "direct cache family",
        exact_mode_0700=False,
    )


def _require_direct_cache_directory(
    path: Path,
    label: str,
    *,
    exact_mode_0700: bool,
) -> tuple[int, int]:
    """校验 direct authority 控制目录 owner、mode 与真实 inode。"""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DailyDirectAuthorityError(
            f"{label} is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise DailyDirectAuthorityError(
            f"{label} must be a real directory"
        )
    expected_uid = os.geteuid()
    if metadata.st_uid != expected_uid:
        raise DailyDirectAuthorityError(f"{label} owner mismatch")
    mode = stat.S_IMODE(metadata.st_mode)
    if exact_mode_0700 and mode != 0o700:
        raise DailyDirectAuthorityError(
            f"{label} must have exact mode 0700"
        )
    if not exact_mode_0700 and mode & 0o022:
        raise DailyDirectAuthorityError(
            f"{label} must not be group/world writable"
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DailyDirectAuthorityError(
            f"{label} changed while opening"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != mode
            or (
                exact_mode_0700
                and stat.S_IMODE(opened.st_mode) != 0o700
            )
            or (
                not exact_mode_0700
                and stat.S_IMODE(opened.st_mode) & 0o022
            )
        ):
            raise DailyDirectAuthorityError(
                f"{label} changed while opening"
            )
    finally:
        os.close(descriptor)
    return metadata.st_dev, metadata.st_ino


def _build_direct_cache_authorities(
    *,
    policy: DailySchedulerPolicy,
    configs: Mapping[str, SchemeConfig],
    target_manifest: Sequence[Mapping[str, object]],
    storage_root: Path,
) -> dict[str, object]:
    cache_ids = {
        scheme_id
        for scheme_id, item in policy.schemes.items()
        if item.cache_spec_fingerprint is not None
    }
    targets_by_base: dict[str, list[Mapping[str, object]]] = {}
    for target in target_manifest:
        base = str(target.get("base_scheme_id") or "")
        if base in cache_ids:
            targets_by_base.setdefault(base, []).append(target)
    if (
        set(targets_by_base) != cache_ids
        or any(len(rows) != 1 for rows in targets_by_base.values())
    ):
        raise DailyDirectAuthorityError(
            "direct cache target manifest differs from policy"
        )

    family_state: dict[
        tuple[str, str], tuple[str, Mapping[str, object]]
    ] = {}
    consumers: dict[str, dict[str, object]] = {}
    for scheme_id in sorted(cache_ids):
        item = policy.schemes[scheme_id]
        config = configs.get(scheme_id)
        if config is None:
            raise DailyDirectAuthorityError(
                f"direct cache config is missing: {scheme_id}"
            )
        try:
            cache_family, tenor = item.cache_group.rsplit(":", 1)
        except ValueError as exc:
            raise DailyDirectAuthorityError(
                f"direct cache group is invalid: {scheme_id}"
            ) from exc
        approved = APPROVED_PHASE_A_CACHE_PUBLISHERS.get(
            cache_family
        )
        if approved is None or approved[0] != tenor:
            raise DailyDirectAuthorityError(
                f"direct cache publisher is not approved: {scheme_id}"
            )
        publisher_id = approved[1]
        family_key = (cache_family, tenor)
        if family_key not in family_state:
            family_root = (
                storage_root
                / safe_path_part(cache_family)
                / safe_path_part(tenor.lower())
            )
            _require_direct_cache_ancestry(
                storage_root,
                family_root,
            )
            loaded, error = _load_current_generation(
                family_root,
                secure=True,
            )
            if loaded is None:
                raise DailyDirectAuthorityError(
                    "direct cache current generation is unavailable: "
                    f"{cache_family}:{tenor} ({error})"
                )
            manifest = loaded.manifest
            input_state = manifest.get("input_state")
            effective = (
                input_state.get("effective_auxiliary")
                if isinstance(input_state, Mapping)
                else None
            )
            if (
                manifest.get("schema_version")
                != GENERATION_MANIFEST_SCHEMA_VERSION
                or manifest.get("abi_version")
                != PHASE_A_CACHE_ABI_VERSION
                or manifest.get("cache_family") != cache_family
                or manifest.get("tenor") != tenor
                or not isinstance(input_state, Mapping)
                or input_state.get("schema_version")
                != INPUT_GENERATION_STATE_SCHEMA_VERSION
                or not isinstance(effective, Mapping)
                or effective.get("schema_version")
                != PROJECTION_SCHEMA_VERSION
            ):
                raise DailyDirectAuthorityError(
                    "direct cache current generation contract drift: "
                    f"{cache_family}:{tenor}"
                )
            proof_identity = effective.get(
                "proof_identity_sha256"
            )
            if (
                not isinstance(proof_identity, str)
                or len(proof_identity) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in proof_identity
                )
            ):
                raise DailyDirectAuthorityError(
                    "direct cache projection proof identity is invalid"
                )
            native = input_state.get("native_generation")
            if not isinstance(native, Mapping):
                raise DailyDirectAuthorityError(
                    "direct cache Native generation binding is missing"
                )
            lineage_authority = {
                "qualification": {
                    "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
                    "cache_family": cache_family,
                    "tenor": tenor,
                    "spec_fingerprint":
                        item.cache_spec_fingerprint,
                    "daily_dependency_lookback_rows": None,
                    "daily_dependency_proof": None,
                }
            }
            try:
                _verify_generation_acceptance_lineage(
                    loaded,
                    trusted_qualification=lineage_authority,
                )
                _validate_generation_acceptance_for_use(
                    loaded,
                    native_generation_binding=dict(native),
                )
                _validate_monotonic_parent_coverage(loaded)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise DailyDirectAuthorityError(
                    "direct cache lineage validation failed: "
                    f"{cache_family}:{tenor}"
                ) from exc
            family_state[family_key] = (
                proof_identity,
                manifest,
            )
        proof_identity, manifest = family_state[family_key]
        if (
            manifest.get("spec_fingerprint")
            != item.cache_spec_fingerprint
        ):
            raise DailyDirectAuthorityError(
                f"direct cache spec fingerprint drift: {scheme_id}"
            )
        target = targets_by_base[scheme_id][0]
        consumers[scheme_id] = {
            "base_scheme_id": scheme_id,
            "cache_consumer_id": scheme_id,
            "scheme_version": config.scheme_version,
            "code_sha256": config.code_hash,
            "config_sha256": config.config_hash,
            "cache_group": item.cache_group,
            "spec_fingerprint": item.cache_spec_fingerprint,
            "publisher_consumer_id": publisher_id,
            "cache_family": cache_family,
            "tenor": tenor,
            "access_mode": (
                "publisher"
                if scheme_id == publisher_id
                else "read_only"
            ),
            "cache_adapter_sha256":
                target["cache_adapter_sha256"],
            "cache_core_sha256": target["cache_core_sha256"],
            "publisher_projection_proof_identity_sha256":
                proof_identity,
            "daily_dependency_lookback_rows": None,
            "daily_dependency_proof": None,
        }
    publishers_by_group: dict[str, list[str]] = {}
    for scheme_id, consumer in consumers.items():
        publisher_id = str(consumer["publisher_consumer_id"])
        publisher = consumers.get(publisher_id)
        if (
            publisher is None
            or publisher["cache_group"] != consumer["cache_group"]
            or publisher["access_mode"] != "publisher"
            or publisher["publisher_consumer_id"] != publisher_id
        ):
            raise DailyDirectAuthorityError(
                f"direct cache publisher graph is invalid: {scheme_id}"
            )
        if consumer["access_mode"] == "publisher":
            publishers_by_group.setdefault(
                str(consumer["cache_group"]),
                [],
            ).append(scheme_id)
    if any(
        len(publishers) != 1
        for publishers in publishers_by_group.values()
    ) or set(publishers_by_group) != {
        str(consumer["cache_group"])
        for consumer in consumers.values()
    }:
        raise DailyDirectAuthorityError(
            "direct cache groups must each have exactly one publisher"
        )
    authority = {
        "schema_version": DIRECT_AUTHORITY_SCHEMA_VERSION,
        "storage_root": os.fspath(storage_root),
        "contract": {
            "manifest_schema_version":
                GENERATION_MANIFEST_SCHEMA_VERSION,
            "input_state_schema_version":
                INPUT_GENERATION_STATE_SCHEMA_VERSION,
            "cache_abi_version": PHASE_A_CACHE_ABI_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "native_generation_type": "native_source",
            "native_generation_schema_version":
                NATIVE_GENERATION_SCHEMA_VERSION,
            "native_exporter_version":
                NATIVE_GENERATION_EXPORTER_VERSION,
        },
        "consumers": consumers,
    }
    for consumer in consumers.values():
        validate_direct_cache_runtime_context(
            {
                "schema_version":
                    "liwei-0616-direct-cache-runtime-context-v1",
                "storage_root": authority["storage_root"],
                "contract": authority["contract"],
                "consumer": consumer,
            }
        )
    return authority


def _validate_monotonic_parent_coverage(generation: object) -> None:
    """安全重开 parent，并要求每个 baseline 覆盖只能单调扩张。"""
    manifest = getattr(generation, "manifest")
    acceptance = manifest.get("generation_acceptance_evidence")
    parent_record = (
        acceptance.get("parent")
        if isinstance(acceptance, Mapping)
        else None
    )
    if parent_record is None:
        return
    if not isinstance(parent_record, Mapping):
        raise ValueError("direct cache parent authority is invalid")
    parent_id = parent_record.get("generation_id")
    if (
        not isinstance(parent_id, str)
        or not parent_id
        or manifest.get("parent_generation_id") != parent_id
    ):
        raise ValueError("direct cache parent identity is invalid")
    parent = _load_generation_directory(
        getattr(generation, "path").parent / parent_id,
        expected_generation_id=parent_id,
        expected_manifest_sha256=parent_record.get(
            "manifest_sha256"
        ),
        secure=True,
    )
    current_caches = getattr(generation, "caches")
    parent_caches = getattr(parent, "caches")
    if set(current_caches) != set(parent_caches):
        raise ValueError("direct cache baseline set is not monotonic")
    for baseline, parent_cache in parent_caches.items():
        if not set(parent_cache["test_dates"]).issubset(
            set(current_caches[baseline]["test_dates"])
        ):
            raise ValueError(
                f"direct cache {baseline} coverage is not monotonic"
            )
