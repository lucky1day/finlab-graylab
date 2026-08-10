from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """读取 YAML mapping；缺少 PyYAML 时使用项目配置子集解析器。"""
    text = path.read_text(encoding="utf-8")
    try:
        yaml_module = importlib.import_module("yaml")
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        return _parse_project_yaml_subset(text)
    raw = yaml_module.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return raw


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
