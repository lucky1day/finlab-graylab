"""Dashboard 后端权威字段与原生前端声明不得漂移。

同一份字段集只在两个无法共享运行时的边界声明：

- `backend/factor_lab_dashboard_semantics.py` —— 权威定义
- `frontend/aifin-shell.js` —— 浏览器端，无构建步骤，同样无法 import

后端加字段时前端不会自动跟随；本测试只锁死这一个必要的跨运行时副本。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import factor_lab_dashboard_semantics as semantics  # noqa: E402

SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _js_literal(name: str) -> set[str]:
    """取前端某个顶层字符串数组字面量。"""
    match = re.search(
        r"\bvar\s+" + re.escape(name) + r"\s*=\s*(\[[^\]]*\])", SHELL_JS.read_text(encoding="utf-8")
    )
    if match is None:
        raise AssertionError(f"{SHELL_JS.name} 未定义 {name}")
    return set(json.loads(match.group(1)))


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
