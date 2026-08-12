"""独立校验器与后端 payload 契约不得漂移。

`scripts/benchmark_factor_lab_dashboard.py` 刻意不 import 仓库模块——它要能对着
已部署实例单独运行，因此字段集是独立声明的。代价是后端加字段时它不会自动跟随，
而它用的是精确集合相等（`set(scheme) != SCHEME_FIELDS`），一旦漂移整个基准测试
会判定所有方案字段不符而失败。本测试锁死这份重复声明的一致性。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import factor_lab_dashboard_semantics as semantics  # noqa: E402

SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_factor_lab_dashboard.py"


def _script_literal(name: str) -> set[str]:
    """取脚本里某个顶层字面量集合，避免 import（脚本有 argparse 副作用）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return {
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant)
                }
    raise AssertionError(f"{SCRIPT.name} 未定义 {name}")


def test_scheme_fields_match() -> None:
    assert _script_literal("SCHEME_FIELDS") == set(semantics.SCHEME_FIELDS)


def test_top_level_fields_match() -> None:
    assert _script_literal("TOP_LEVEL_FIELDS") == set(semantics.TOP_LEVEL_FIELDS)


def test_row_fields_match() -> None:
    assert _script_literal("ROW_FIELDS") == set(semantics.ROW_FIELDS)


def test_backtest_fields_match() -> None:
    assert _script_literal("BACKTEST_FIELDS") == set(semantics.BACKTEST_FIELDS)


def test_task_types_match() -> None:
    assert _script_literal("TASK_TYPES") == set(semantics.VALID_TASK_TYPES)


def test_signal_statuses_match() -> None:
    assert _script_literal("SIGNAL_STATUSES") == set(semantics.VALID_SIGNAL_STATUSES)
