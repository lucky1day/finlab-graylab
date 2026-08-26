from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from harness.context import GateContext
from shared.scheme_config_schema import validate_config
from harness.contracts.onboarding_policy import validate_onboarding_policy
from harness.contracts.import_rules import (
    CORE_DB_CALL_NAMES,
    CORE_FORBIDDEN_QUALIFIED_CALLS,
    DANGEROUS_CORE_IMPORTS,
    RuleViolation,
    backtest_runner_boundary_violations,
    call_violations,
    cross_scheme_imports,
    dangerous_imports,
    file_write_violations,
    legacy_active_import_violations,
    parse_python,
    predict_input_artifact_bypass_violations,
    predict_import_whitelist_violations,
    qualified_call_violations,
    root_benchmark_runtime_dependency_violations,
    sql_write_literals,
)
from harness.contracts.predict_contract import validate_predict_module
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.native_maintenance_admission_gate import (
    NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY,
    native_business_identity_snapshot,
)
from harness.result import Evidence, GateResult, GateStatus
from scheduler.discovery import load_scheme_config
from shared.scheme_config_loader import load_yaml_mapping


class StaticGate(Gate):
    name = "static"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        project_root = ctx.project_root
        scheme_dir = project_root / "schemes" / ctx.scheme_id
        evidence: list[Evidence] = []
        errors: list[str] = []

        dir_name_ok = scheme_dir.name == ctx.scheme_id and scheme_dir.exists()
        evidence.append(Evidence("dir_name_ok", dir_name_ok))
        if not dir_name_ok:
            errors.append(f"schemes/{ctx.scheme_id}: scheme directory missing or mismatched")

        required_files = {
            "config.yaml": scheme_dir / "config.yaml",
            "predict.py": scheme_dir / "predict.py",
            "__init__.py": scheme_dir / "__init__.py",
            "core/__init__.py": scheme_dir / "core" / "__init__.py",
        }
        missing_files = [name for name, path in required_files.items() if not path.exists()]
        evidence.append(Evidence("required_files", {"missing": missing_files}))
        errors.extend(f"{ctx.scheme_id}: missing required file {name}" for name in missing_files)

        config_raw: dict[str, Any] = {}
        config_errors: list[str] = []
        config_path = required_files["config.yaml"]
        if config_path.exists():
            try:
                config_raw = load_yaml_mapping(config_path)
                load_scheme_config(config_path)
            except Exception as exc:
                config_errors.append(str(exc))
            config_errors.extend(validate_config(config_raw, scheme_dir.name))
        else:
            config_errors.append("config.yaml is missing")
        evidence.append(Evidence("config_schema_errors", config_errors))
        errors.extend(f"{_display_path(config_path, project_root)}: {message}" for message in config_errors)

        if (
            not config_errors
            and config_raw.get("runtime_type", "native_adapter") == "native_adapter"
        ):
            try:
                evidence.append(
                    Evidence(
                        NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY,
                        native_business_identity_snapshot(
                            scheme_id=config_raw["scheme_id"],
                            runtime_type=config_raw.get(
                                "runtime_type", "native_adapter"
                            ),
                            horizon=config_raw["horizon"],
                            task_type=config_raw["task_type"],
                            frequency=config_raw["frequency"],
                            tenors=config_raw["tenors"],
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(
                    f"{_display_path(config_path, project_root)}: "
                    f"native business identity snapshot invalid: {exc}"
                )

        policy_errors = (
            validate_onboarding_policy(
                project_root,
                ctx.scheme_id,
                str(config_raw.get("runtime_type", "native_adapter")),
            )
            if config_raw
            else []
        )
        evidence.append(Evidence("onboarding_policy_allowed", not policy_errors))
        evidence.append(Evidence("onboarding_policy_errors", policy_errors))
        errors.extend(policy_errors)

        predict_path = required_files["predict.py"]
        predict_facts = {"scheme_id_const_ok": False, "run_signature_ok": False, "shared_input_imported": False}
        predict_violations: list[RuleViolation] = []
        if predict_path.exists():
            predict_violations, predict_facts = validate_predict_module(predict_path, ctx.scheme_id)
            predict_tree = parse_python(predict_path)
            predict_violations.extend(
                predict_import_whitelist_violations(predict_path, predict_tree, ctx.scheme_id)
            )
            predict_violations.extend(predict_input_artifact_bypass_violations(predict_path, predict_tree))
            predict_violations.extend(file_write_violations(predict_path, predict_tree))
            predict_violations.extend(legacy_active_import_violations(predict_path, predict_tree))
        evidence.extend(Evidence(key, value) for key, value in predict_facts.items())

        predict_no_db_errors = [v for v in predict_violations if _is_predict_write_violation(v)]
        evidence.append(Evidence("predict_no_db_writes", not predict_no_db_errors))
        evidence.append(Evidence("predict_contract_violations", [v.format(project_root) for v in predict_violations]))
        errors.extend(v.format(project_root) for v in predict_violations)

        core_violations = self._core_violations(scheme_dir)
        evidence.append(Evidence("dangerous_core_imports", [v.format(project_root) for v in core_violations["imports"]]))
        evidence.append(Evidence("core_db_calls", [v.format(project_root) for v in core_violations["calls"]]))
        evidence.append(Evidence("core_sql_write_literals", [v.format(project_root) for v in core_violations["sql_writes"]]))
        evidence.append(Evidence("core_file_writes", [v.format(project_root) for v in core_violations["file_writes"]]))
        evidence.append(Evidence("core_legacy_imports", [v.format(project_root) for v in core_violations["legacy_imports"]]))
        evidence.append(Evidence("core_zero_db", not any(core_violations.values())))
        for items in core_violations.values():
            errors.extend(v.format(project_root) for v in items)

        cross_imports = self._cross_scheme_violations(scheme_dir, project_root, ctx.scheme_id)
        evidence.append(Evidence("cross_scheme_imports", [v.format(project_root) for v in cross_imports]))
        errors.extend(v.format(project_root) for v in cross_imports)

        backtest_errors = self._backtest_runner_errors(config_raw, project_root)
        evidence.append(
            Evidence("backtest_runner_contract", not backtest_errors)
        )
        errors.extend(backtest_errors)

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _core_violations(self, scheme_dir: Path) -> dict[str, list[RuleViolation]]:
        result: dict[str, list[RuleViolation]] = {
            "imports": [],
            "calls": [],
            "sql_writes": [],
            "file_writes": [],
            "legacy_imports": [],
        }
        core_dir = scheme_dir / "core"
        if not core_dir.exists():
            return result
        for path in sorted(core_dir.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = parse_python(path)
            is_legacy = path.name.startswith("legacy_")
            # legacy 仍受“只读归档”约束：禁 DB import/调用、禁写文件、禁跨方案；
            # 仅放宽“可保留历史算法代码”。这里对 legacy 与 active 施加相同的 DB/写文件检查。
            result["imports"].extend(dangerous_imports(path, tree, DANGEROUS_CORE_IMPORTS))
            result["calls"].extend(call_violations(path, tree, CORE_DB_CALL_NAMES | set()))
            result["calls"].extend(
                qualified_call_violations(path, tree, CORE_FORBIDDEN_QUALIFIED_CALLS)
            )
            result["sql_writes"].extend(sql_write_literals(path, tree))
            result["file_writes"].extend(file_write_violations(path, tree))
            if not is_legacy:
                # active core 不得 import legacy_* 模块。
                result["legacy_imports"].extend(legacy_active_import_violations(path, tree))
        return result

    def _cross_scheme_violations(self, scheme_dir: Path, project_root: Path, scheme_id: str) -> list[RuleViolation]:
        violations: list[RuleViolation] = []
        for path in sorted(scheme_dir.rglob("*.py")):
            tree = parse_python(path)
            violations.extend(cross_scheme_imports(path, tree, scheme_id, project_root))
        return violations

    def _backtest_runner_errors(self, config_raw: dict[str, Any], project_root: Path) -> list[str]:
        runner = ((config_raw.get("backtest") or {}) if isinstance(config_raw, dict) else {}).get("runner")
        if not runner:
            return []
        runner_path = project_root / (str(runner).replace(".", "/") + ".py")
        if not runner_path.exists():
            return [f"{runner}: backtest runner file does not exist"]
        tree = parse_python(runner_path)
        errors: list[str] = []
        runner_paths = self._backtest_runner_dependency_paths(runner_path, project_root)
        for path in runner_paths:
            path_tree = tree if path == runner_path else parse_python(path)
            errors.extend(v.format(project_root) for v in backtest_runner_boundary_violations(path, path_tree))
        if str(config_raw.get("status", "")).strip() == "active":
            for path in runner_paths:
                path_tree = tree if path == runner_path else parse_python(path)
                errors.extend(
                    v.format(project_root) for v in root_benchmark_runtime_dependency_violations(path, path_tree)
                )
        return errors

    def _backtest_runner_dependency_paths(self, runner_path: Path, project_root: Path) -> list[Path]:
        """返回入口 runner 及其 backtests.* 本地依赖。"""
        paths: list[Path] = []
        seen: set[Path] = set()

        def visit(path: Path) -> None:
            resolved = path.resolve()
            if resolved in seen or not path.exists():
                return
            seen.add(resolved)
            paths.append(path)
            tree = parse_python(path)
            for candidate in _backtest_dependency_candidates(path, tree, project_root):
                visit(candidate)

        visit(runner_path)
        return paths


def _backtest_dependency_candidates(current_path: Path, tree: ast.AST, project_root: Path) -> list[Path]:
    """解析 backtests 本地 import 的候选文件，覆盖 from backtests import x 形态。"""
    candidates: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                normalized = alias.name.lstrip(".")
                if normalized == "backtests" or normalized.startswith("backtests."):
                    candidates.extend(_module_path_candidates(normalized, project_root))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _resolve_import_from_module(current_path, node, project_root)
        if module == "backtests":
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidates.extend(_module_path_candidates(f"backtests.{alias.name}", project_root))
            continue
        if module.startswith("backtests."):
            candidates.extend(_module_path_candidates(module, project_root))
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidates.extend(_module_path_candidates(f"{module}.{alias.name}", project_root))
    return candidates


def _resolve_import_from_module(current_path: Path, node: ast.ImportFrom, project_root: Path) -> str:
    if node.level <= 0:
        return node.module or ""
    package = _package_name_for_path(current_path, project_root)
    if not package:
        return node.module or ""
    parts = package.split(".")
    if node.level > len(parts):
        return node.module or ""
    base_parts = parts[: len(parts) - node.level + 1]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _package_name_for_path(path: Path, project_root: Path) -> str:
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return ""
    module_parts = list(rel.with_suffix("").parts)
    if not module_parts:
        return ""
    return ".".join(module_parts[:-1])


def _module_path_candidates(module: str, project_root: Path) -> list[Path]:
    module_path = project_root / module.replace(".", "/")
    return [module_path.with_suffix(".py"), module_path / "__init__.py"]


def _is_predict_write_violation(violation: RuleViolation) -> bool:
    return (
        "dangerous import" in violation.message
        or "dangerous call" in violation.message
        or "SQL write keyword" in violation.message
    )


def _display_path(path: Path, project_root: Path) -> Path:
    try:
        return path.relative_to(project_root)
    except ValueError:
        return path
