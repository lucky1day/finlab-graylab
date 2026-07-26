from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path
from typing import Iterable


DANGEROUS_CORE_IMPORTS = {
    "sqlalchemy",
    "pymysql",
    "psycopg2",
    "requests",
    "urllib",
    "httpx",
    "socket",
    "subprocess",
    "scheduler",
    "backend",
    "backtests",
    "shared.input_artifacts",
    "shared.data_service",
    "shared.db_config",
    "shared.repository",
}
CORE_DB_CALL_NAMES = {
    "create_engine",
    "create_sqlalchemy_engine",
    "read_sql",
    "text",
}
# core 禁止的限定调用（attr 形式：module.attr(...)），避免误伤 json.load / yaml.load 等。
CORE_FORBIDDEN_QUALIFIED_CALLS = {
    ("os", "system"),
    ("pickle", "load"),
    ("joblib", "load"),
}
# core 禁止的文件写入 mode（open(..., mode) 的写模式）
CORE_WRITE_OPEN_MODES = {"w", "a", "wb", "ab", "w+", "a+", "x", "xb"}
# core 禁止的写文件方法名（Path.write_text / Path.write_bytes 等）
CORE_WRITE_METHOD_NAMES = {"write_text", "write_bytes"}
CORE_EXPORT_METHOD_NAMES = {"to_csv", "to_parquet", "to_excel"}
WRITE_CALL_NAMES = {
    "insert_run_predictions",
    "write_run_log",
    "execute_scheme",
    "replace_backtest_predictions",
    "insert_reproduction_check",
}
PREDICT_DANGEROUS_IMPORTS = {
    "scheduler.repository",
    "scheduler.executor",
}
# predict.py 允许的 import 白名单（shared.* / 框架层面）。
# shared.contracts 在本仓库的真实模块名为 shared.models（PredictionRecord 等契约类型所在）。
PREDICT_ALLOWED_SHARED_IMPORTS = {
    "shared.input_artifacts",
    "shared.models",
    "shared.calendar_service",
    "shared.prediction_context",
    "shared.signal_policy",
    "shared.weekly_average_source_evidence",
    "shared.weekly_average_lgbm_source_runner",
    "shared.weekly_average_lgbm_predict_adapter",
    "shared.monthly_predict_adapter",
    "shared.daily_0629_predict_adapter",
}
SQL_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
SQL_WRITE_PATTERN = re.compile(r"\b(" + "|".join(SQL_WRITE_KEYWORDS) + r")\b", re.IGNORECASE)
LIVE_TABLE_NAMES = {
    "t_scheme_predictions",
    "t_scheme_runs",
    "t_scheme_run_log",
    "t_scheme_actuals",
    "t_scheme_weekly_actuals",
    "t_scheme_monthly_actuals",
}
BACKTEST_FORBIDDEN_IMPORTS = {"scheduler", "backend"}
ROOT_BENCHMARK_ALLOWED_NAME_MARKERS = ("SOURCE_EVIDENCE", "SOURCE_ARCHIVE", "EXTERNAL_SOURCE", "AUDIT")
UNKNOWN_PATH_SEGMENT = "<unknown>"
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
    "scheduler": frozenset({"schemes", "backend", "backtests", "harness"}),
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
    ``REPOSITORY_PRODUCTION_LAYER_ROOTS`` 中，因此不会被误当成生产层。Native 的函数
    签名、写库、I/O 和输入契约仍由 onboarding ``StaticGate`` 的细粒度规则负责。
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


def _qualified_call(func: ast.AST) -> tuple[str, str] | None:
    """提取 module.attr 形式的限定调用，返回 (module, attr)。"""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def qualified_call_violations(
    path: Path, tree: ast.AST, qualified_calls: Iterable[tuple[str, str]]
) -> list[RuleViolation]:
    """检测形如 os.system()/pickle.load()/joblib.load() 的限定危险调用。"""
    targets = set(qualified_calls)
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = _qualified_call(node.func)
        if qualified in targets:
            violations.append(
                RuleViolation(path, node.lineno, f"dangerous call: {qualified[0]}.{qualified[1]}()")
            )
    return violations


def file_write_violations(path: Path, tree: ast.AST) -> list[RuleViolation]:
    """检测写文件调用：open 写模式、Path.write_* 与 DataFrame.to_* 导出。"""
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name in CORE_WRITE_METHOD_NAMES:
            violations.append(RuleViolation(path, node.lineno, f"file write call: {name}()"))
            continue
        if name in CORE_EXPORT_METHOD_NAMES:
            violations.append(RuleViolation(path, node.lineno, f"file export call: {name}()"))
            continue
        if name == "open":
            mode = _open_mode(node)
            if mode is not None and mode in CORE_WRITE_OPEN_MODES:
                violations.append(RuleViolation(path, node.lineno, f"file write call: open(mode={mode!r})"))
    return violations


def _open_mode(node: ast.Call) -> str | None:
    """读取 open(...) 的 mode 实参（位置第 2 个或关键字 mode）。"""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        return node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def predict_import_whitelist_violations(
    path: Path, tree: ast.AST, scheme_id: str
) -> list[RuleViolation]:
    """predict.py 仅允许 import：白名单 shared.* / 本方案 schemes.{id}.* / 标准库与第三方库。

    被显式拦截的是 shared.* 中不在白名单的模块（如 shared.data_service）以及框架层
    (scheduler / backend / backtests) 的越层 import；跨方案 import 由 cross_scheme_imports 另行处理。
    """
    violations: list[RuleViolation] = []
    forbidden_framework_prefixes = ("scheduler", "backend", "backtests")
    own_scheme_prefix = f"schemes.{scheme_id}"
    for imported, line in import_names(tree):
        normalized = imported.lstrip(".")
        top = normalized.split(".")[0]
        if normalized.startswith("shared."):
            if not _matches_allowed_shared(normalized):
                violations.append(
                    RuleViolation(path, line, f"predict import not in whitelist: {normalized}")
                )
            continue
        if normalized == "shared":
            violations.append(RuleViolation(path, line, "predict import not in whitelist: shared"))
            continue
        if top in forbidden_framework_prefixes:
            violations.append(
                RuleViolation(path, line, f"predict import not in whitelist: {normalized}")
            )
            continue
        if normalized.startswith("schemes."):
            # 本方案内部模块允许；跨方案由 cross_scheme_imports 捕获。
            if normalized == own_scheme_prefix or normalized.startswith(f"{own_scheme_prefix}."):
                continue
            # 其它 schemes.* 交由跨方案规则报错，这里不重复。
            continue
        # 标准库 / 第三方库（如 datetime / pathlib / pandas / sqlalchemy 的类型）不视为越层。
    return violations


def predict_input_artifact_bypass_violations(path: Path, tree: ast.AST) -> list[RuleViolation]:
    """禁止 predict.py 通过 input_artifacts 暴露的 data_service 绕过输入 artifact。"""
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = ("." * int(node.level) + (node.module or "")).lstrip(".")
            if module == "shared.input_artifacts":
                for alias in node.names:
                    if alias.name == "data_service":
                        violations.append(
                            RuleViolation(
                                path,
                                node.lineno,
                                "predict import forbidden: shared.input_artifacts.data_service",
                            )
                        )
        elif isinstance(node, ast.Call):
            if _is_data_service_build_from_db_call(node.func):
                violations.append(
                    RuleViolation(
                        path,
                        node.lineno,
                        f"predict call forbidden: data_service.{node.func.attr}()",
                    )
                )
    return violations


def _is_data_service_build_from_db_call(func: ast.AST) -> bool:
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    return (
        func.value.id == "data_service"
        and func.attr.startswith("build_")
        and func.attr.endswith("_from_db")
    )


def backtest_runner_boundary_violations(path: Path, tree: ast.AST) -> list[RuleViolation]:
    """Backtest runner 只能通过 backtests.repository 写 t_backtest_*，不得触碰 live 写库边界。"""
    violations: list[RuleViolation] = []
    violations.extend(backtest_forbidden_imports(path, tree))
    violations.extend(live_table_sql_write_literals(path, tree))
    return violations


def root_benchmark_runtime_dependency_violations(path: Path, tree: ast.AST) -> list[RuleViolation]:
    """active backtest runner 不得把外部证据归档当作普通运行输入。

    旧根 benchmarks/ 和新 source_evidence/ 都只允许作为显式 source-evidence /
    audit / archive 路径保留；runner 默认输入必须来自 shared.input_artifacts。
    """
    violations: list[RuleViolation] = []
    tainted_names = _external_source_assigned_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            source_kind = _external_source_path_kind(node.value)
            source_kind = source_kind or _tainted_external_source_kind(node.value, tainted_names)
            if source_kind and not _targets_mark_source_evidence(node.targets):
                violations.append(_external_source_path_violation(path, node.lineno, source_kind))
        elif isinstance(node, ast.AnnAssign):
            source_kind = _external_source_path_kind(node.value) if node.value is not None else None
            source_kind = source_kind or _tainted_external_source_kind(node.value, tainted_names)
            if (
                source_kind
                and not _targets_mark_source_evidence([node.target])
            ):
                violations.append(_external_source_path_violation(path, node.lineno, source_kind))
        elif isinstance(node, ast.Return):
            source_kind = _external_source_path_kind(node.value) if node.value is not None else None
            if source_kind:
                violations.append(_external_source_path_violation(path, node.lineno, source_kind))
        elif isinstance(node, ast.Expr):
            source_kind = _external_source_path_kind(node.value)
            if source_kind:
                violations.append(_external_source_path_violation(path, node.lineno, source_kind))
        elif isinstance(node, ast.FunctionDef):
            for default in node.args.defaults:
                source_kind = _external_source_path_kind(default)
                if source_kind:
                    violations.append(_external_source_path_violation(path, node.lineno, source_kind))
            for default in (item for item in node.args.kw_defaults if item):
                source_kind = _external_source_path_kind(default)
                if source_kind:
                    violations.append(_external_source_path_violation(path, node.lineno, source_kind))
    violations.extend(_tainted_source_return_violations(path, tree, tainted_names))
    return violations


def _external_source_path_violation(path: Path, line: int, source_kind: str) -> RuleViolation:
    if source_kind == "source_evidence":
        message = "source_evidence path is source-evidence only; use shared.input_artifacts for active backtest input"
    else:
        message = "root benchmarks path is source-evidence only; use shared.input_artifacts for active backtest input"
    return RuleViolation(
        path,
        line,
        message,
    )


def _targets_mark_source_evidence(targets: Iterable[ast.AST]) -> bool:
    names: list[str] = []
    for target in targets:
        names.extend(_target_names(target))
    return bool(names) and all(_name_marks_source_evidence(name) for name in names)


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_target_names(item))
        return names
    return []


def _name_marks_source_evidence(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in ROOT_BENCHMARK_ALLOWED_NAME_MARKERS)


def _external_source_assigned_names(tree: ast.AST) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            source_kind = _external_source_path_kind(node.value)
            if source_kind:
                for name in _target_names_for_taint(node.targets):
                    names[name] = source_kind
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            source_kind = _external_source_path_kind(node.value)
            if source_kind:
                for name in _target_names_for_taint([node.target]):
                    names[name] = source_kind
    return names


def _target_names_for_taint(targets: Iterable[ast.AST]) -> list[str]:
    names: list[str] = []
    for target in targets:
        for name in _target_names(target):
            names.append(name)
    return names


def _tainted_external_source_kind(node: ast.AST | None, tainted_names: dict[str, str]) -> str | None:
    if node is None:
        return None
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in tainted_names:
            return tainted_names[child.id]
    return None


def _tainted_source_return_violations(
    path: Path,
    tree: ast.AST,
    tainted_names: dict[str, str],
) -> list[RuleViolation]:
    if not tainted_names:
        return []

    violations: list[RuleViolation] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_stack.append(node.name)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.function_stack.append(node.name)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Return(self, node: ast.Return) -> None:
            source_kind = _tainted_external_source_kind(node.value, tainted_names)
            if source_kind and not self._in_explicit_source_context():
                violations.append(_external_source_path_violation(path, node.lineno, source_kind))
            self.generic_visit(node)

        def _in_explicit_source_context(self) -> bool:
            return bool(self.function_stack) and _name_marks_source_evidence(self.function_stack[-1])

    Visitor().visit(tree)
    return violations


def _external_source_path_kind(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    path_segments = _literal_path_segments(node)
    if path_segments is not None and not isinstance(node, ast.Constant):
        return _segments_external_source_kind(path_segments)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.replace("\\", "/").strip()
        if (
            value.startswith("benchmarks/")
            or value == "./benchmarks"
            or value.startswith("./benchmarks/")
            or ("/benchmarks/" in value and "/schemes/" not in value)
        ):
            return "root_benchmarks"
        if (
            value.startswith("source_evidence/")
            or value == "./source_evidence"
            or value.startswith("./source_evidence/")
            or "/source_evidence/" in value
        ):
            return "source_evidence"
        return None
    for child in ast.iter_child_nodes(node):
        source_kind = _external_source_path_kind(child)
        if source_kind:
            return source_kind
    return None


def _literal_path_segments(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _literal_path_segments(node.left)
        right = _literal_path_segments(node.right)
        if left is None and right is None:
            return None
        return (left or [UNKNOWN_PATH_SEGMENT]) + (right or [UNKNOWN_PATH_SEGMENT])
    if isinstance(node, ast.Call) and call_name(node.func) == "benchmark_source_evidence_root":
        return ["source_evidence", "benchmark_batches", UNKNOWN_PATH_SEGMENT]
    if isinstance(node, ast.Call) and call_name(node.func) == "Path" and node.args:
        segments: list[str] = []
        for arg in node.args:
            arg_segments = _literal_path_segments(arg)
            segments.extend(arg_segments or [UNKNOWN_PATH_SEGMENT])
        return segments
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.replace("\\", "/").strip()
        return [part for part in value.split("/") if part and part != "."]
    if isinstance(node, (ast.Name, ast.Attribute)):
        return [UNKNOWN_PATH_SEGMENT]
    return None


def _segments_external_source_kind(segments: list[str]) -> str | None:
    for index, segment in enumerate(segments):
        if segment == "source_evidence":
            return "source_evidence"
        if segment != "benchmarks":
            continue
        prior_segments = set(segments[:index])
        if "schemes" not in prior_segments:
            return "root_benchmarks"
    return None


def backtest_forbidden_imports(path: Path, tree: ast.AST) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                normalized = alias.name.lstrip(".")
                if _is_backtest_forbidden_import(normalized):
                    violations.append(RuleViolation(path, node.lineno, f"dangerous import: {normalized}"))
        elif isinstance(node, ast.ImportFrom):
            module = ("." * int(node.level) + (node.module or "")).lstrip(".")
            if _is_backtest_forbidden_import(module):
                for alias in node.names:
                    target = f"{module}.{alias.name}" if alias.name != "*" else module
                    violations.append(RuleViolation(path, node.lineno, f"dangerous import: {target}"))
    return violations


def _is_backtest_forbidden_import(normalized: str) -> bool:
    return any(normalized == prefix or normalized.startswith(f"{prefix}.") for prefix in BACKTEST_FORBIDDEN_IMPORTS)


def live_table_sql_write_literals(path: Path, tree: ast.AST) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not SQL_WRITE_PATTERN.search(node.value):
            continue
        lowered = node.value.lower()
        for table in sorted(LIVE_TABLE_NAMES):
            if table.lower() in lowered:
                violations.append(
                    RuleViolation(path, node.lineno, f"live table SQL write literal: {table}")
                )
                break
    return violations


def _matches_allowed_shared(normalized: str) -> bool:
    for allowed in PREDICT_ALLOWED_SHARED_IMPORTS:
        if normalized == allowed or normalized.startswith(f"{allowed}."):
            return True
    return False


def legacy_active_import_violations(path: Path, tree: ast.AST) -> list[RuleViolation]:
    """active 模块（predict.py / 非 legacy core）不得 import legacy_* 模块。

    覆盖三种写法：
      import a.legacy_x
      from a.legacy_x import y
      from a.core import legacy_x   （legacy_ 作为被导入名）
    """
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.lstrip(".")
                if _has_legacy_segment(module):
                    violations.append(
                        RuleViolation(path, node.lineno, f"active module imports legacy module: {module}")
                    )
        elif isinstance(node, ast.ImportFrom):
            module = ("." * int(node.level) + (node.module or "")).lstrip(".")
            if _has_legacy_segment(module):
                violations.append(
                    RuleViolation(path, node.lineno, f"active module imports legacy module: {module}")
                )
                continue
            for alias in node.names:
                if alias.name.startswith("legacy_"):
                    target = f"{module}.{alias.name}" if module else alias.name
                    violations.append(
                        RuleViolation(path, node.lineno, f"active module imports legacy module: {target}")
                    )
    return violations


def _has_legacy_segment(dotted: str) -> bool:
    return any(part.startswith("legacy_") for part in dotted.split(".") if part)


def sql_write_literals(path: Path, tree: ast.AST) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        match = SQL_WRITE_PATTERN.search(node.value)
        if match:
            violations.append(RuleViolation(path, node.lineno, f"SQL write keyword literal: {match.group(1).upper()}"))
    return violations


def cross_scheme_imports(
    path: Path,
    tree: ast.AST,
    scheme_id: str,
    project_root: Path | None = None,
) -> list[RuleViolation]:
    if project_root is None:
        project_root = _project_root_for_scheme_path(path)
    violations: list[RuleViolation] = []
    for imported, line in resolved_import_names(path, tree, project_root):
        normalized = imported.lstrip(".")
        if not normalized.startswith("schemes."):
            continue
        parts = normalized.split(".")
        if len(parts) >= 2 and parts[1] != scheme_id:
            violations.append(RuleViolation(path, line, f"cross-scheme import: {normalized}"))
    return violations


def _project_root_for_scheme_path(path: Path) -> Path:
    for parent in path.parents:
        if parent.name == "schemes":
            return parent.parent
    return path.parent
