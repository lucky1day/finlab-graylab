from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # forecast_env keeps scheduler dry-run lean and may not include PyYAML.
    yaml = None

from harness.contracts.config_schema import validate_config
from shared.versioning import compute_code_hash, compute_config_hash, compute_manifest_hash, compute_scheme_version


logger = logging.getLogger(__name__)
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
    errors = validate_config(raw, config_path.parent.name)
    if errors:
        raise ValueError(f"{config_path}: " + "; ".join(errors))
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
        task_type=str(raw["task_type"]),
        tenors=[str(item) for item in tenors],
        frequency=str(raw["frequency"]),
        schedule=SchemeSchedule(
            cron=str(schedule_raw["cron"]),
            timezone=str(schedule_raw.get("timezone", "Asia/Shanghai")),
            timeout_sec=int(schedule_raw["timeout_sec"]) if schedule_raw.get("timeout_sec") is not None else None,
        ),
        entry_point=str(raw.get("entry_point", "predict.run")),
        status=str(raw["status"]),
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

    lines = _yaml_subset_lines(text)

    def next_indent(pos: int) -> int | None:
        if pos >= len(lines):
            return None
        return lines[pos][0]

    def parse_block(pos: int, indent: int) -> tuple[Any, int]:
        if pos >= len(lines) or lines[pos][0] < indent:
            return {}, pos
        if lines[pos][1].startswith("- "):
            return parse_list(pos, indent)
        return parse_mapping(pos, indent)

    def parse_mapping(pos: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while pos < len(lines):
            current_indent, line = lines[pos]
            if current_indent < indent:
                break
            if current_indent > indent:
                break
            if line.startswith("- "):
                break
            if ":" not in line:
                pos += 1
                continue
            key, value = _split_key_value(line)
            pos += 1
            if value == "":
                child_indent = next_indent(pos)
                if child_indent is None or child_indent <= current_indent:
                    result[key] = {}
                else:
                    child, pos = parse_block(pos, child_indent)
                    result[key] = child
            else:
                result[key] = _parse_scalar(value, literal_ast)
        return result, pos

    def parse_list(pos: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while pos < len(lines):
            current_indent, line = lines[pos]
            if current_indent < indent:
                break
            if current_indent > indent:
                break
            if not line.startswith("- "):
                break
            item_text = line[2:].strip()
            pos += 1
            if item_text == "":
                child_indent = next_indent(pos)
                if child_indent is None or child_indent <= current_indent:
                    result.append({})
                else:
                    child, pos = parse_block(pos, child_indent)
                    result.append(child)
                continue
            if ":" in item_text:
                key, value = _split_key_value(item_text)
                item: dict[str, Any] = {}
                if value == "":
                    child_indent = next_indent(pos)
                    if child_indent is None or child_indent <= current_indent:
                        item[key] = {}
                    else:
                        child, pos = parse_block(pos, child_indent)
                        item[key] = child
                else:
                    item[key] = _parse_scalar(value, literal_ast)
                while pos < len(lines):
                    child_indent = next_indent(pos)
                    if child_indent is None or child_indent <= current_indent:
                        break
                    if lines[pos][1].startswith("- "):
                        break
                    extra, pos = parse_mapping(pos, child_indent)
                    item.update(extra)
                result.append(item)
            else:
                result.append(_parse_scalar(item_text, literal_ast))
        return result, pos

    parsed, _ = parse_block(0, lines[0][0] if lines else 0)
    if not isinstance(parsed, dict):
        raise ValueError("config.yaml must contain a mapping")
    return parsed


def _yaml_subset_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, raw_line.strip()))
    return lines


def _split_key_value(line: str) -> tuple[str, str]:
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _parse_scalar(value: str, literal_ast_module) -> Any:
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    try:
        return literal_ast_module.literal_eval(value)
    except Exception:
        pass
    if value.isdigit():
        return int(value)
    return value
