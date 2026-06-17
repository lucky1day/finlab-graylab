from __future__ import annotations

import ast
import re
from dataclasses import dataclass
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
}
SQL_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
SQL_WRITE_PATTERN = re.compile(r"\b(" + "|".join(SQL_WRITE_KEYWORDS) + r")\b", re.IGNORECASE)
LIVE_TABLE_NAMES = {
    "t_scheme_predictions",
    "t_scheme_runs",
    "t_scheme_run_log",
    "t_scheme_actuals",
    "t_scheme_weekly_actuals",
}
BACKTEST_FORBIDDEN_IMPORTS = {"scheduler", "backend"}
ROOT_BENCHMARK_ALLOWED_NAME_MARKERS = ("SOURCE_EVIDENCE", "SOURCE_ARCHIVE", "EXTERNAL_SOURCE", "AUDIT")
UNKNOWN_PATH_SEGMENT = "<unknown>"


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
    """active backtest runner 不得把根 benchmarks/ 当作普通运行输入。

    根 benchmarks/ 只允许作为外部 source-evidence/audit/archive 归档路径保留；实际
    runner 默认输入必须来自 shared.input_artifacts。
    """
    violations: list[RuleViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if _contains_root_benchmarks_literal(node.value) and not _targets_mark_source_evidence(node.targets):
                violations.append(_root_benchmark_violation(path, node.lineno))
        elif isinstance(node, ast.AnnAssign):
            if (
                node.value is not None
                and _contains_root_benchmarks_literal(node.value)
                and not _targets_mark_source_evidence([node.target])
            ):
                violations.append(_root_benchmark_violation(path, node.lineno))
        elif isinstance(node, ast.Return):
            if node.value is not None and _contains_root_benchmarks_literal(node.value):
                violations.append(_root_benchmark_violation(path, node.lineno))
        elif isinstance(node, ast.Expr):
            if _contains_root_benchmarks_literal(node.value):
                violations.append(_root_benchmark_violation(path, node.lineno))
        elif isinstance(node, ast.FunctionDef):
            if any(_contains_root_benchmarks_literal(default) for default in node.args.defaults):
                violations.append(_root_benchmark_violation(path, node.lineno))
            if any(_contains_root_benchmarks_literal(default) for default in node.args.kw_defaults if default):
                violations.append(_root_benchmark_violation(path, node.lineno))
    return violations


def _root_benchmark_violation(path: Path, line: int) -> RuleViolation:
    return RuleViolation(
        path,
        line,
        "root benchmarks path is source-evidence only; use shared.input_artifacts for active backtest input",
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


def _contains_root_benchmarks_literal(node: ast.AST) -> bool:
    path_segments = _literal_path_segments(node)
    if path_segments is not None and not isinstance(node, ast.Constant):
        return _segments_reference_root_benchmarks(path_segments)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.replace("\\", "/").strip()
        return (
            value.startswith("benchmarks/")
            or value == "./benchmarks"
            or value.startswith("./benchmarks/")
            or ("/benchmarks/" in value and "/schemes/" not in value)
        )
    return any(_contains_root_benchmarks_literal(child) for child in ast.iter_child_nodes(node))


def _literal_path_segments(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _literal_path_segments(node.left)
        right = _literal_path_segments(node.right)
        if left is None and right is None:
            return None
        return (left or [UNKNOWN_PATH_SEGMENT]) + (right or [UNKNOWN_PATH_SEGMENT])
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


def _segments_reference_root_benchmarks(segments: list[str]) -> bool:
    for index, segment in enumerate(segments):
        if segment != "benchmarks":
            continue
        prior_segments = set(segments[:index])
        return "schemes" not in prior_segments
    return False


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
