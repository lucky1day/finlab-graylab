from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from harness.config_loader import load_config_raw, try_load_scheme_config
from harness.context import GateContext
from harness.contracts.config_schema import validate_config
from harness.contracts.import_rules import (
    CORE_DB_CALL_NAMES,
    DANGEROUS_CORE_IMPORTS,
    RuleViolation,
    call_violations,
    cross_scheme_imports,
    dangerous_imports,
    has_shared_input_artifacts_import,
    parse_python,
    sql_write_literals,
)
from harness.contracts.predict_contract import validate_predict_module
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


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
        evidence.append(Evidence("dir_name_ok", dir_name_ok, str(_display_path(scheme_dir, project_root))))
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
                config_raw = load_config_raw(config_path)
                try_load_scheme_config(config_path)
            except Exception as exc:
                config_errors.append(str(exc))
            config_errors.extend(validate_config(config_raw, scheme_dir.name))
        else:
            config_errors.append("config.yaml is missing")
        evidence.append(Evidence("config_schema_errors", config_errors))
        errors.extend(f"{_display_path(config_path, project_root)}: {message}" for message in config_errors)

        predict_path = required_files["predict.py"]
        predict_facts = {"scheme_id_const_ok": False, "run_signature_ok": False, "shared_input_imported": False}
        predict_violations: list[RuleViolation] = []
        if predict_path.exists():
            predict_violations, predict_facts = validate_predict_module(predict_path, ctx.scheme_id, project_root)
        evidence.extend(Evidence(key, value) for key, value in predict_facts.items())

        predict_no_db_errors = [v for v in predict_violations if _is_predict_write_violation(v)]
        evidence.append(Evidence("predict_no_db_writes", not predict_no_db_errors))
        evidence.append(Evidence("predict_contract_violations", [v.format(project_root) for v in predict_violations]))
        errors.extend(v.format(project_root) for v in predict_violations)

        core_violations = self._core_violations(scheme_dir, project_root)
        evidence.append(Evidence("dangerous_core_imports", [v.format(project_root) for v in core_violations["imports"]]))
        evidence.append(Evidence("core_db_calls", [v.format(project_root) for v in core_violations["calls"]]))
        evidence.append(Evidence("core_sql_write_literals", [v.format(project_root) for v in core_violations["sql_writes"]]))
        evidence.append(Evidence("core_zero_db", not any(core_violations.values())))
        for items in core_violations.values():
            errors.extend(v.format(project_root) for v in items)

        cross_imports = self._cross_scheme_violations(scheme_dir, project_root, ctx.scheme_id)
        evidence.append(Evidence("cross_scheme_imports", [v.format(project_root) for v in cross_imports]))
        errors.extend(v.format(project_root) for v in cross_imports)

        backtest_errors = self._backtest_input_artifact_errors(config_raw, project_root)
        evidence.append(Evidence("backtest_imports_input_artifacts", not backtest_errors, "; ".join(backtest_errors) or None))
        errors.extend(backtest_errors)

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _core_violations(self, scheme_dir: Path, project_root: Path) -> dict[str, list[RuleViolation]]:
        result = {"imports": [], "calls": [], "sql_writes": []}
        core_dir = scheme_dir / "core"
        if not core_dir.exists():
            return result
        for path in sorted(core_dir.glob("*.py")):
            if path.name.startswith("legacy_") or path.name == "__init__.py":
                continue
            tree = parse_python(path)
            result["imports"].extend(dangerous_imports(path, tree, DANGEROUS_CORE_IMPORTS))
            result["calls"].extend(call_violations(path, tree, CORE_DB_CALL_NAMES | set()))
            result["sql_writes"].extend(sql_write_literals(path, tree))
        return result

    def _cross_scheme_violations(self, scheme_dir: Path, project_root: Path, scheme_id: str) -> list[RuleViolation]:
        violations: list[RuleViolation] = []
        for path in sorted(scheme_dir.rglob("*.py")):
            if path.name.startswith("legacy_"):
                continue
            tree = parse_python(path)
            violations.extend(cross_scheme_imports(path, tree, scheme_id))
        return violations

    def _backtest_input_artifact_errors(self, config_raw: dict[str, Any], project_root: Path) -> list[str]:
        runner = ((config_raw.get("backtest") or {}) if isinstance(config_raw, dict) else {}).get("runner")
        if not runner:
            return []
        runner_path = project_root / (str(runner).replace(".", "/") + ".py")
        if not runner_path.exists():
            return [f"{runner}: backtest runner file does not exist"]
        tree = parse_python(runner_path)
        if has_shared_input_artifacts_import(tree):
            return []
        return [f"{_display_path(runner_path, project_root)}: backtest runner must import shared.input_artifacts"]


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
