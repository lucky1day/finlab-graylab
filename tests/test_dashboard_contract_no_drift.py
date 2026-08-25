"""Dashboard 后端权威字段与原生前端声明不得漂移。

同一份字段集只在两个无法共享运行时的边界声明：

- `backend/factor_lab_dashboard_semantics.py` —— 权威定义
- `frontend/aifin-shell.js` —— 浏览器端，无构建步骤，同样无法 import

后端加字段时前端不会自动跟随；本测试只锁死这一个必要的跨运行时副本。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from backend import factor_lab_dashboard_semantics as semantics

SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _js_literal(name: str) -> set[str]:
    """取前端某个顶层字符串数组字面量。"""
    match = re.search(
        r"\bvar\s+" + re.escape(name) + r"\s*=\s*(\[[^\]]*\])", SHELL_JS.read_text(encoding="utf-8")
    )
    if match is None:
        raise AssertionError(f"{SHELL_JS.name} 未定义 {name}")
    return set(json.loads(match.group(1)))


def test_frontend_dashboard_contract_matches_backend() -> None:
    contracts = (
        ("DASHBOARD_SCHEME_FIELDS", semantics.SCHEME_FIELDS),
        ("DASHBOARD_TOP_FIELDS", semantics.TOP_LEVEL_FIELDS),
        ("DASHBOARD_ROW_FIELDS", semantics.ROW_FIELDS),
        ("DASHBOARD_BACKTEST_FIELDS", semantics.BACKTEST_FIELDS),
        ("DASHBOARD_TASK_TYPES", semantics.VALID_TASK_TYPES),
        ("DASHBOARD_SIGNAL_STATUSES", semantics.VALID_SIGNAL_STATUSES),
    )
    for javascript_name, backend_values in contracts:
        assert _js_literal(javascript_name) == set(backend_values), javascript_name
