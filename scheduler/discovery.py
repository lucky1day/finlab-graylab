from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scheduler.deployment_scope import filter_schemes_for_configured_target
from shared.blackbox_v2.versioning import compute_blackbox_config_hash
from shared.blackbox_v2.contracts import BlackboxMetadata
from shared.scheme_config_loader import load_yaml_mapping
from shared.scheme_config_schema import validate_config
from shared.versioning import (
    compute_code_hash,
    compute_config_hash,
    compute_manifest_hash,
    compute_scheme_version,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"


@dataclass(frozen=True)
class SchemeSchedule:
    """方案调度配置。"""

    cron: str
    timezone: str = "Asia/Shanghai"
    timeout_sec: int | None = None


@dataclass(frozen=True)
class SchemeConfig:
    """方案配置。"""

    scheme_id: str
    name: str
    description: str
    horizon: int
    task_type: str
    tenors: list[str]
    frequency: str
    schedule: SchemeSchedule
    status: str
    path: Path
    code_hash: str
    config_hash: str
    manifest_hash: str | None
    scheme_version: str
    runtime_type: str
    version_status: str
    algorithm_version: str | None
    contract_version: str | None
    runtime_profile: str | None
    data_schema_version: str | None
    target_rule: str | None
    delivery_script: Path | None
    delivery_metadata: Path | None
    environment_fingerprint: str | None
    data_snapshot_id: str | None
    input_source: str = "legacy_db"
    factor_input_mode: str | None = None
    blackbox_metadata: BlackboxMetadata | None = None
    owner: str | None = None
    incremental_state: bool = False


def _require_mapping(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_scheme_config(config_path: Path) -> SchemeConfig:
    """读取 immutable config.yaml 中声明的方案身份。"""
    return _load_declared_scheme_config(config_path)


def _load_declared_scheme_config(config_path: Path) -> SchemeConfig:
    """读取单个方案 config.yaml 中声明的配置，不含本机生命周期状态。"""
    raw = load_yaml_mapping(config_path)
    errors = validate_config(raw, config_path.parent.name)
    if errors:
        raise ValueError(f"{config_path}: " + "; ".join(errors))
    runtime_type = str(raw.get("runtime_type", "native_adapter"))
    schedule_raw = _require_mapping(raw.get("schedule", {}), config_path)
    scheme_id = str(raw["scheme_id"]).strip()
    if scheme_id != config_path.parent.name:
        raise ValueError(f"{config_path}: scheme_id must match directory name")
    if runtime_type == "blackbox_v2":
        return _load_blackbox_config(config_path, raw, schedule_raw)

    tenors = raw.get("tenors")
    if not isinstance(tenors, list) or not tenors:
        raise ValueError(f"{config_path}: tenors must be a non-empty list")

    scheme_dir = config_path.parent
    code_hash = compute_code_hash(scheme_dir)
    config_hash = compute_config_hash(config_path)
    manifest_hash = compute_manifest_hash(scheme_dir)

    return SchemeConfig(
        scheme_id=scheme_id,
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        horizon=int(raw["horizon"]),
        task_type=str(raw["task_type"]),
        tenors=[str(item) for item in tenors],
        frequency=str(raw["frequency"]),
        schedule=SchemeSchedule(
            cron=str(schedule_raw["cron"]),
            timezone=str(schedule_raw.get("timezone", "Asia/Shanghai")),
            timeout_sec=int(schedule_raw["timeout_sec"]) if schedule_raw.get("timeout_sec") is not None else None,
        ),
        status=str(raw["status"]),
        path=scheme_dir,
        code_hash=code_hash,
        config_hash=config_hash,
        manifest_hash=manifest_hash,
        scheme_version=compute_scheme_version(code_hash, config_hash),
        runtime_type="native_adapter",
        version_status=str(raw.get("version_status", raw["status"])),
        algorithm_version=None,
        contract_version=None,
        runtime_profile=None,
        data_schema_version=None,
        target_rule=str(raw["target_rule"]) if raw.get("target_rule") is not None else None,
        delivery_script=None,
        delivery_metadata=None,
        environment_fingerprint=None,
        data_snapshot_id=None,
        input_source=str(raw.get("input_source", "legacy_db")),
        factor_input_mode=None,
    )


def _load_blackbox_config(config_path: Path, raw: dict[str, Any], schedule_raw: dict[str, Any]) -> SchemeConfig:
    from shared.blackbox_v2.contracts import load_metadata_bytes
    from shared.blackbox_v2.intake import validate_canonical_layout

    scheme_dir = config_path.parent
    _scheme_path, canonical_config_path, canonical_delivery_dir = (
        validate_canonical_layout(scheme_dir)
    )
    if canonical_config_path != config_path.resolve():
        raise ValueError(f"{config_path}: config path is not canonical")
    delivery_entries = sorted(canonical_delivery_dir.iterdir())
    if len(delivery_entries) != 2 or any(
        not item.is_file() or item.is_symlink() for item in delivery_entries
    ):
        raise ValueError(
            f"{config_path}: Blackbox V2 delivery must contain exactly two regular files"
        )
    delivery_raw = _require_mapping(raw.get("delivery", {}), config_path)
    script_path = (scheme_dir / str(delivery_raw["script"])).resolve()
    metadata_path = (scheme_dir / str(delivery_raw["metadata"])).resolve()
    if scheme_dir.resolve() not in script_path.parents or scheme_dir.resolve() not in metadata_path.parents:
        raise ValueError(f"{config_path}: delivery paths must stay inside scheme directory")
    if not script_path.is_file() or not metadata_path.is_file():
        raise ValueError(f"{config_path}: Blackbox V2 delivery files are missing")
    try:
        metadata_payload = metadata_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{metadata_path}: cannot read metadata: {exc}") from exc
    metadata = load_metadata_bytes(metadata_payload, source=str(metadata_path))
    if metadata.scheme_id != scheme_dir.name:
        raise ValueError(f"{metadata_path}: scheme_id must match directory name")
    if script_path.name != f"{metadata.scheme_id}.py" or metadata_path.name != f"{metadata.scheme_id}.json":
        raise ValueError(f"{config_path}: delivery filenames must match scheme_id")
    code_hash = _hash_file(script_path)
    config_hash = compute_blackbox_config_hash(raw)
    manifest_hash = hashlib.sha256(metadata_payload).hexdigest()
    display_name = raw.get("display_name")
    resolved_name = (
        str(display_name).strip()
        if isinstance(display_name, str) and display_name.strip()
        else metadata.name
    )
    return SchemeConfig(
        scheme_id=metadata.scheme_id,
        name=resolved_name,
        description=metadata.description or "",
        horizon=metadata.horizon,
        task_type=metadata.task_type,
        tenors=[metadata.target_tenor],
        frequency=metadata.frequency,
        schedule=SchemeSchedule(
            cron=str(schedule_raw["cron"]),
            timezone=str(schedule_raw.get("timezone", "Asia/Shanghai")),
            timeout_sec=int(schedule_raw["timeout_sec"]) if schedule_raw.get("timeout_sec") is not None else None,
        ),
        status=str(raw["status"]),
        path=scheme_dir,
        code_hash=code_hash,
        config_hash=config_hash,
        manifest_hash=manifest_hash,
        scheme_version=compute_scheme_version(code_hash, config_hash, manifest_hash),
        runtime_type="blackbox_v2",
        version_status=str(raw["version_status"]),
        algorithm_version=metadata.algorithm_version,
        contract_version=metadata.schema_version,
        runtime_profile=str(raw["runtime_profile"]),
        data_schema_version=str(raw["data_schema_version"]),
        target_rule=metadata.target_rule,
        delivery_script=script_path,
        delivery_metadata=metadata_path,
        environment_fingerprint=None,
        data_snapshot_id=None,
        input_source=str(raw["input_source"]),
        factor_input_mode=str(raw.get("factor_input_mode", "legacy_v1")),
        blackbox_metadata=metadata,
        owner=metadata.owner,
        incremental_state=raw.get("incremental_state", False),
    )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_schemes(schemes_root: Path = SCHEMES_ROOT) -> list[SchemeConfig]:
    """扫描 schemes/ 下的方案配置。"""
    configs: list[SchemeConfig] = []
    for config_path in sorted(schemes_root.glob("*/config.yaml")):
        if config_path.parent.name.startswith("_"):
            continue
        configs.append(load_scheme_config(config_path))
    return filter_schemes_for_configured_target(configs)
