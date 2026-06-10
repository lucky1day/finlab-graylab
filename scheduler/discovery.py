from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # forecast_env keeps scheduler dry-run lean and may not include PyYAML.
    yaml = None

from shared.versioning import compute_code_hash, compute_config_hash, compute_manifest_hash, compute_scheme_version


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"


@dataclass(frozen=True)
class SchemeSchedule:
    """方案调度配置。"""

    cron: str
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class SchemeConfig:
    """方案配置。"""

    scheme_id: str
    name: str
    description: str
    horizon: int
    tenors: list[str]
    frequency: str
    schedule: SchemeSchedule
    entry_point: str
    status: str
    path: Path
    code_hash: str
    config_hash: str
    manifest_hash: str | None
    scheme_version: str


def _require_mapping(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_scheme_config(config_path: Path) -> SchemeConfig:
    """读取单个方案 config.yaml。"""
    raw = _require_mapping(_load_yaml(config_path), config_path)
    schedule_raw = _require_mapping(raw.get("schedule", {}), config_path)
    scheme_id = str(raw["scheme_id"]).strip()
    if scheme_id != config_path.parent.name:
        raise ValueError(f"{config_path}: scheme_id must match directory name")
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
        tenors=[str(item) for item in tenors],
        frequency=str(raw.get("frequency", "daily")),
        schedule=SchemeSchedule(
            cron=str(schedule_raw["cron"]),
            timezone=str(schedule_raw.get("timezone", "Asia/Shanghai")),
        ),
        entry_point=str(raw.get("entry_point", "predict.run")),
        status=str(raw.get("status", "active")),
        path=scheme_dir,
        code_hash=code_hash,
        config_hash=config_hash,
        manifest_hash=manifest_hash,
        scheme_version=compute_scheme_version(code_hash, config_hash),
    )


def discover_schemes(schemes_root: Path = SCHEMES_ROOT, strict: bool = False) -> list[SchemeConfig]:
    """扫描 schemes/ 下的方案配置。"""
    configs: list[SchemeConfig] = []
    for config_path in sorted(schemes_root.glob("*/config.yaml")):
        if config_path.parent.name.startswith("_"):
            continue
        try:
            configs.append(load_scheme_config(config_path))
        except Exception:
            if strict:
                raise
            logger.exception("Skip invalid scheme config: %s", config_path)
    return configs


def active_schemes(schemes_root: Path = SCHEMES_ROOT) -> list[SchemeConfig]:
    """返回 active 状态方案。"""
    return [cfg for cfg in discover_schemes(schemes_root) if cfg.status == "active"]


def _load_yaml(config_path: Path) -> Any:
    text = config_path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    return _parse_project_yaml_subset(text)


def _parse_project_yaml_subset(text: str) -> dict[str, Any]:
    import ast as literal_ast

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            current[key] = child
            stack.append((indent, child))
        else:
            current[key] = _parse_scalar(value, literal_ast)
    return root


def _parse_scalar(value: str, literal_ast_module) -> Any:
    try:
        return literal_ast_module.literal_eval(value)
    except Exception:
        pass
    if value.isdigit():
        return int(value)
    return value
