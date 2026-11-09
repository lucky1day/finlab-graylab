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

_PATH_COMPONENT_CALLS = {
    "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath",
    "pathlib.PurePosixPath", "pathlib.PureWindowsPath", "os.path.join",
}
_TWO_PATH_CALLS = {
    "os.rename", "os.replace", "os.link", "os.symlink",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move",
}
_ONE_PATH_CALLS = {
    "open", "builtins.open", "io.open", "os.open", "os.listdir", "os.scandir",
    "os.stat", "os.lstat", "os.access", "os.chdir", "os.mkdir", "os.makedirs",
    "os.unlink", "os.remove", "os.rmdir", "os.path.exists", "os.path.isfile",
    "os.path.isdir", "os.path.abspath", "os.path.realpath", "shutil.rmtree",
    "pandas.read_csv", "pandas.read_excel", "pandas.read_json", "pandas.read_parquet",
    "pandas.read_pickle", "pandas.read_feather", "pandas.read_table",
    "numpy.load", "numpy.loadtxt", "numpy.genfromtxt", "numpy.save", "numpy.savetxt",
}
_PATH_OUTPUT_METHODS = {"to_csv", "to_excel", "to_json", "to_parquet", "to_pickle", "to_feather"}
_PATH_KEYWORDS = {
    "file", "path", "filename", "fname", "filepath_or_buffer", "path_or_buf",
    "src", "dst", "excel_writer",
}


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
    nodes = list(ast.walk(tree))
    aliases = _import_bindings(nodes)
    absolute_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and _absolute_path_prefix(node.value):
            absolute_names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.value is not None and _absolute_path_prefix(node.value)
        ):
            absolute_names.add(node.target.id)
    for node in nodes:
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
            root, separator, suffix = name.partition(".")
            names = {name, *(original + separator + suffix for original in aliases.get(root, ()))}
            for resolved in names:
                if (
                    resolved in FORBIDDEN_QUALIFIED_CALLS
                    or resolved.removeprefix("builtins.") in FORBIDDEN_CALLS
                ):
                    violations.append(f"line {node.lineno}: forbidden call {resolved}")
            if any(
                _absolute_path_prefix(argument)
                or (isinstance(argument, ast.Name) and argument.id in absolute_names)
                for argument in _path_arguments(node, name, names)
            ):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
        elif (
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and (
                _absolute_path_prefix(node.right)
                or (isinstance(node.right, ast.Name) and node.right.id in absolute_names)
            )
        ):
            violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value.strip()
            if ABSOLUTE_PATH_PATTERN.match(literal):
                violations.append(f"line {node.lineno}: hard-coded absolute path is forbidden")
            if RELATIVE_TRAVERSAL_PATTERN.search(literal):
                violations.append(
                    f"line {node.lineno}: relative path traversal literal is forbidden"
                )
    return sorted(set(violations))


def _import_bindings(nodes: list[ast.AST]) -> dict[str, set[str]]:
    """收集显式 import 及 from-import 的原始名称。"""
    bindings: dict[str, set[str]] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                bindings.setdefault(alias.asname or root, set()).add(alias.name if alias.asname else root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bindings.setdefault(alias.asname or alias.name, set()).add(f"{node.module}.{alias.name}")
    return bindings


def _path_arguments(call: ast.Call, name: str, names: set[str]) -> tuple[ast.AST, ...]:
    """只检查文件入口的路径参数，避免把分隔符或消息当作路径。"""
    method = name.rsplit(".", 1)[-1] if "." in name else ""
    if names & _PATH_COMPONENT_CALLS or method == "joinpath":
        positional = call.args
    elif names & _TWO_PATH_CALLS:
        positional = call.args[:2]
    elif names & _ONE_PATH_CALLS or method in _PATH_OUTPUT_METHODS:
        positional = call.args[:1]
    else:
        return ()
    return (*positional, *(item.value for item in call.keywords if item.arg in _PATH_KEYWORDS))


def _absolute_path_prefix(node: ast.AST) -> bool:
    """检查可静态判定的路径开头，不误判 f-string 中部或结尾的斜杠。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        literal = node.value.strip()
        return literal.startswith(("/", "\\\\", "file:/")) or bool(ABSOLUTE_PATH_PATTERN.match(literal))
    if isinstance(node, ast.JoinedStr) and node.values:
        return _absolute_path_prefix(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _absolute_path_prefix(node.left)
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""
