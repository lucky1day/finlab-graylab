from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DANGEROUS_CORE_IMPORTS = {
    "sqlalchemy",
    "pymysql",
    "scheduler",
    "shared.input_artifacts",
}
CORE_DB_CALL_NAMES = {
    "create_engine",
    "create_sqlalchemy_engine",
    "read_sql",
    "text",
}
WRITE_CALL_NAMES = {
    "upsert_predictions",
    "write_run_log",
    "execute_scheme",
    "replace_backtest_predictions",
    "replace_backtest_monthly_metrics",
    "insert_reproduction_check",
}
PREDICT_DANGEROUS_IMPORTS = {
    "scheduler.repository",
    "scheduler.executor",
}
SQL_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
SQL_WRITE_PATTERN = re.compile(r"\b(" + "|".join(SQL_WRITE_KEYWORDS) + r")\b", re.IGNORECASE)


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


def has_import(tree: ast.AST, module_name: str) -> bool:
    for imported, _ in import_names(tree):
        normalized = imported.lstrip(".")
        if normalized == module_name or normalized.startswith(f"{module_name}."):
            return True
    return False


def has_shared_input_artifacts_import(tree: ast.AST) -> bool:
    return has_import(tree, "shared.input_artifacts")


def dangerous_imports(path: Path, tree: ast.AST, modules: Iterable[str]) -> list[RuleViolation]:
    dangerous = set(modules)
    violations: list[RuleViolation] = []
    for imported, line in import_names(tree):
        normalized = imported.lstrip(".")
        for module in dangerous:
            if normalized == module or normalized.startswith(f"{module}."):
                violations.append(RuleViolation(path, line, f"dangerous import: {normalized}"))
                break
    return violations


def call_violations(path: Path, tree: ast.AST, call_names: Iterable[str]) -> list[RuleViolation]:
    names = set(call_names)
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name in names:
            violations.append(RuleViolation(path, node.lineno, f"dangerous call: {name}()"))
    return violations


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def sql_write_literals(path: Path, tree: ast.AST) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        match = SQL_WRITE_PATTERN.search(node.value)
        if match:
            violations.append(RuleViolation(path, node.lineno, f"SQL write keyword literal: {match.group(1).upper()}"))
    return violations


def cross_scheme_imports(path: Path, tree: ast.AST, scheme_id: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for imported, line in import_names(tree):
        normalized = imported.lstrip(".")
        if not normalized.startswith("schemes."):
            continue
        parts = normalized.split(".")
        if len(parts) >= 2 and parts[1] != scheme_id:
            violations.append(RuleViolation(path, line, f"cross-scheme import: {normalized}"))
    return violations
