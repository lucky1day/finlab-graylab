from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def load_config_raw(config_path: Path) -> dict[str, Any]:
    """读取 config.yaml；优先 PyYAML，缺失时使用项目 config 子集解析。"""
    try:
        yaml_module = importlib.import_module("yaml")
    except ModuleNotFoundError:
        return parse_project_yaml_subset(config_path.read_text(encoding="utf-8"))
    raw = yaml_module.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: config.yaml must contain a mapping")
    return raw


def try_load_scheme_config(config_path: Path) -> Any | None:
    """尽力复用 scheduler.discovery.load_scheme_config；依赖不可用时返回 None。"""
    try:
        discovery = importlib.import_module("scheduler.discovery")
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            return None
        raise
    return discovery.load_scheme_config(config_path)


def parse_project_yaml_subset(text: str) -> dict[str, Any]:
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
    # YAML 式布尔值
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    # Python 字面量
    try:
        return literal_ast_module.literal_eval(value)
    except Exception:
        pass
    if value.isdigit():
        return int(value)
    return value
