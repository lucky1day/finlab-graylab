from __future__ import annotations

import ast
from pathlib import Path


def test_production_callers_do_not_import_scheduler_persistence_directly() -> None:
    """业务写入继续只经 repository 统一入口。"""
    project_root = Path(__file__).resolve().parents[1]
    checked_roots = (
        project_root / "backend",
        project_root / "harness",
        project_root / "scripts",
        project_root / "scheduler",
    )
    violations: list[str] = []
    for root in checked_roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or "persistence" in path.parts:
                continue
            if path == project_root / "scheduler" / "repository.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (
                    node.module or ""
                ).startswith("scheduler.persistence"):
                    violations.append(str(path.relative_to(project_root)))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("scheduler.persistence"):
                            violations.append(str(path.relative_to(project_root)))
    assert violations == []
