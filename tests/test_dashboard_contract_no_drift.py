"""dashboard payload 契约的所有独立副本不得漂移。

同一份字段集在三处独立声明，且三处都用精确集合相等：

- `backend/factor_lab_dashboard_semantics.py` —— 权威定义
- `scripts/benchmark_factor_lab_dashboard.py` —— 纯标准库、零仓库依赖，要能对着
  已部署实例单独运行，因此不能 import 权威定义
- `frontend/aifin-shell.js` —— 浏览器端，无构建步骤，同样无法 import

后端加字段时另外两处不会自动跟随，而它们都是硬失败：脚本抛
`scheme fields are invalid`，前端抛 `fields must match v1 exactly` 导致整个
dashboard 渲染不出来。本测试锁死三份声明的一致性。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import factor_lab_dashboard_semantics as semantics  # noqa: E402

SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_factor_lab_dashboard.py"
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _js_literal(name: str) -> set[str]:
    """取前端某个顶层字符串数组字面量。"""
    match = re.search(
        r"\bvar\s+" + re.escape(name) + r"\s*=\s*(\[[^\]]*\])", SHELL_JS.read_text(encoding="utf-8")
    )
    if match is None:
        raise AssertionError(f"{SHELL_JS.name} 未定义 {name}")
    return set(json.loads(match.group(1)))


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


def test_frontend_scheme_fields_match() -> None:
    assert _js_literal("DASHBOARD_SCHEME_FIELDS") == set(semantics.SCHEME_FIELDS)


def test_frontend_top_level_fields_match() -> None:
    assert _js_literal("DASHBOARD_TOP_FIELDS") == set(semantics.TOP_LEVEL_FIELDS)


def test_frontend_row_fields_match() -> None:
    assert _js_literal("DASHBOARD_ROW_FIELDS") == set(semantics.ROW_FIELDS)


def test_frontend_backtest_fields_match() -> None:
    assert _js_literal("DASHBOARD_BACKTEST_FIELDS") == set(semantics.BACKTEST_FIELDS)


def test_frontend_task_types_match() -> None:
    assert _js_literal("DASHBOARD_TASK_TYPES") == set(semantics.VALID_TASK_TYPES)


def test_frontend_signal_statuses_match() -> None:
    assert _js_literal("DASHBOARD_SIGNAL_STATUSES") == set(semantics.VALID_SIGNAL_STATUSES)
