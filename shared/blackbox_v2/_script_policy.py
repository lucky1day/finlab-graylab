"""Blackbox 交付脚本的静态校验策略。策略摘要只绑定本模块。"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path


FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "ftplib",
    "http",
    "mysql",
    "pymysql",
    "psycopg",
    "requests",
    "socket",
    "sqlalchemy",
    "sqlite3",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_QUALIFIED_CALLS = {"os.system", "os.popen", "os.spawnl", "os.spawnv"}
ABSOLUTE_PATH_PATTERN = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:[\\/])")
RELATIVE_TRAVERSAL_PATTERN = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
SCRIPT_VALIDATOR_POLICY_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validate_delivery_script(script_path: str | Path) -> None:
    """校验一份已定位的 Blackbox 交付脚本。"""
    script = Path(script_path)
    if not script.is_file() or script.is_symlink() or script.suffix != ".py":
        raise ValueError("Blackbox V2 delivery script must be one regular .py file")
    try:
        tree = ast.parse(
            script.read_text(encoding="utf-8"),
            filename=str(script),
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"delivery script syntax error: {exc}") from exc
    violations = _script_violations(tree)
    if violations:
        raise ValueError("unsafe Blackbox V2 delivery script: " + "; ".join(violations))


def _script_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"line {node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"line {node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS or name in FORBIDDEN_QUALIFIED_CALLS:
                violations.append(f"line {node.lineno}: forbidden call {name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value.strip()
            if ABSOLUTE_PATH_PATTERN.match(literal):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
            if RELATIVE_TRAVERSAL_PATTERN.search(literal):
                violations.append(
                    f"line {node.lineno}: relative path traversal literal is forbidden"
                )
    return sorted(set(violations))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
