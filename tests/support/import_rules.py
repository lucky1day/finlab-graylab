from __future__ import annotations

import ast
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path


REPOSITORY_PRODUCTION_LAYER_ROOTS = (
    "shared",
    "schemes",
    "scheduler",
    "backend",
    "backtests",
    "harness",
)
REPOSITORY_FORBIDDEN_LAYER_IMPORTS = {
    "shared": frozenset({"schemes", "scheduler", "backend", "backtests", "harness"}),
    "scheduler": frozenset(
        {"schemes", "backend", "backtests", "harness", "scripts"}
    ),
    "backend": frozenset({"schemes", "backtests", "harness"}),
    "backtests": frozenset({"scheduler", "backend", "harness"}),
    "schemes": frozenset({"scheduler", "backend", "backtests", "harness"}),
    "harness": frozenset({"scripts"}),
}


@dataclass(frozen=True)
class RuleViolation:
    path: Path
    line: int
    message: str

    def format(self, project_root: Path) -> str:
        try:
            display_path = self.path.relative_to(project_root)
        except ValueError:
            display_path = self.path
        return f"{display_path}:{self.line}: {self.message}"


def repository_layer_import_violations(project_root: Path) -> list[RuleViolation]:
    """扫描仓库生产 Python 模块的分层 import 违规。

    该扫描器覆盖显式的静态 import 边；测试、运行产物和未纳入分层图的管理脚本不在
    ``REPOSITORY_PRODUCTION_LAYER_ROOTS`` 中，因此不会被误当成生产层。该扫描只检查
    模块依赖方向，不验证函数签名、I/O、SQL 或运行时输入。
    """
    violations: list[RuleViolation] = []
    for layer in REPOSITORY_PRODUCTION_LAYER_ROOTS:
        layer_root = project_root / layer
        if not layer_root.is_dir():
            continue
        for path in sorted(layer_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = parse_python(path)
            violations.extend(_layer_import_violations(path, tree, layer, project_root))
    return sorted(
        violations,
        key=lambda item: (
            _display_repository_path(item.path, project_root),
            item.line,
            item.message,
        ),
    )


def _layer_import_violations(
    path: Path,
    tree: ast.AST,
    source_layer: str,
    project_root: Path,
) -> list[RuleViolation]:
    forbidden = REPOSITORY_FORBIDDEN_LAYER_IMPORTS.get(source_layer, frozenset())
    violations: list[RuleViolation] = []
    source_label = _repository_source_label(path, source_layer, project_root)
    for imported, line in resolved_import_names(path, tree, project_root):
        normalized = imported.lstrip(".")
        if not normalized:
            continue
        target_layer = normalized.split(".", 1)[0]
        is_forbidden = target_layer in forbidden
        if (
            source_layer == "backtests"
            and target_layer == "schemes"
            and _is_scheme_predict_import(tree, normalized, line)
        ):
            is_forbidden = True
        if (
            source_layer == "schemes"
            and target_layer == "shared"
            and _is_native_core_path(path, project_root)
        ):
            is_forbidden = True
        if (
            source_layer == "schemes"
            and target_layer == "schemes"
            and _is_cross_scheme_import(path, normalized, project_root)
        ):
            is_forbidden = True
        if not is_forbidden:
            continue
        violations.append(
            RuleViolation(
                path,
                line,
                f"forbidden layer import: {source_label} -> {normalized}",
            )
        )
    return violations


def _is_scheme_predict_import(tree: ast.AST, normalized: str, line: int) -> bool:
    parts = normalized.split(".")
    if "predict" in parts[2:]:
        return True
    if len(parts) != 2:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.lineno != line:
            continue
        module = (node.module or "").lstrip(".")
        if module != normalized:
            continue
        return any(alias.name == "predict" for alias in node.names)
    return False


def _is_native_core_path(path: Path, project_root: Path) -> bool:
    try:
        relative = path.relative_to(project_root / "schemes")
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[1] == "core"


def _is_cross_scheme_import(path: Path, normalized: str, project_root: Path) -> bool:
    try:
        source_scheme_id = path.relative_to(project_root / "schemes").parts[0]
    except (ValueError, IndexError):
        return False
    imported_parts = normalized.split(".")
    return len(imported_parts) >= 2 and imported_parts[1] != source_scheme_id


def _repository_source_label(path: Path, source_layer: str, project_root: Path) -> str:
    if source_layer != "schemes":
        return source_layer
    try:
        relative = path.relative_to(project_root / "schemes")
    except ValueError:
        return source_layer
    if len(relative.parts) < 2:
        return source_layer
    return f"schemes.{relative.parts[0]}"


def _display_repository_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def parse_python(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        raise


def import_names(tree: ast.AST) -> list[tuple[str, int]]:
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = "." * int(node.level) + (node.module or "")
            names.append((module, node.lineno))
    return names


def resolved_import_names(
    path: Path,
    tree: ast.AST,
    project_root: Path,
) -> list[tuple[str, int]]:
    """按源模块 package 解析相对 import，返回可比较的绝对模块名。"""
    try:
        source_package = ".".join(path.relative_to(project_root).parent.parts)
    except ValueError:
        source_package = ""

    names: list[tuple[str, int]] = []
    for imported, line in import_names(tree):
        if not imported.startswith(".") or not source_package:
            names.append((imported, line))
            continue
        try:
            names.append((resolve_name(imported, source_package), line))
        except ImportError:
            # 越过顶层的相对 import 本身会在运行时失败；保留原值，让既有规则继续
            # 对其显式 module 部分进行 fail-closed 检查。
            names.append((imported, line))
    return names
