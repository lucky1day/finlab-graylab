from __future__ import annotations

import ast
from pathlib import Path

from harness.contracts.import_rules import (
    PREDICT_DANGEROUS_IMPORTS,
    WRITE_CALL_NAMES,
    RuleViolation,
    call_violations,
    dangerous_imports,
    has_shared_input_artifacts_import,
    sql_write_literals,
)


def validate_predict_module(
    predict_path: Path,
    scheme_id: str,
    tree: ast.Module,
) -> tuple[list[RuleViolation], dict[str, bool]]:
    """使用已解析 AST 校验 predict.py；不 import、不执行方案模块。"""
    facts = {
        "scheme_id_const_ok": _scheme_id_const(tree, scheme_id),
        "run_signature_ok": _run_signature_ok(tree),
        "shared_input_imported": has_shared_input_artifacts_import(tree),
    }
    violations: list[RuleViolation] = []
    if not facts["scheme_id_const_ok"]:
        violations.append(RuleViolation(predict_path, 1, f"SCHEME_ID must equal {scheme_id!r}"))
    if not facts["run_signature_ok"]:
        violations.append(RuleViolation(predict_path, 1, "run signature must be run(predict_date)"))
    if not facts["shared_input_imported"]:
        violations.append(RuleViolation(predict_path, 1, "predict.py must import shared.input_artifacts"))
    violations.extend(dangerous_imports(predict_path, tree, PREDICT_DANGEROUS_IMPORTS))
    violations.extend(call_violations(predict_path, tree, WRITE_CALL_NAMES))
    violations.extend(sql_write_literals(predict_path, tree))
    return violations, facts


def _scheme_id_const(tree: ast.Module, scheme_id: str) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCHEME_ID":
                    return isinstance(node.value, ast.Constant) and node.value.value == scheme_id
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "SCHEME_ID":
            return isinstance(node.value, ast.Constant) and node.value.value == scheme_id
    return False


def _run_signature_ok(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "run":
            continue
        args = node.args
        return (
            len(args.posonlyargs) == 0
            and len(args.args) == 1
            and args.args[0].arg == "predict_date"
            and len(args.kwonlyargs) == 0
            and args.vararg is None
            and args.kwarg is None
            and len(args.defaults) == 0
        )
    return False
